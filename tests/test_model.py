# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Integration tests using the installed QGIS / GDAL providers, not provider mocks.

Run: QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m unittest discover -v
"""

import gc
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.PyQt.QtCore import QObject, pyqtSignal, Qt, QSettings
from qgis.PyQt.QtWidgets import QAction, QDialog, QInputDialog, QMainWindow, QMessageBox
from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer, QgsVectorLayerUtils
from qgis.gui import QgsAdvancedDigitizingDockWidget, QgsMapCanvas, QgsMessageBar

from acalc.dialogs import FeatureDialog
from acalc.layers import geometry_from_rows, load_layer, validate_layer, vertex_rows
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, definition, new_id
from acalc.storage import ModelError, ModelStore

_OWNS_APP = QgsApplication.instance() is None
APP = QgsApplication.instance() or QgsApplication([], False)
if _OWNS_APP:
    import atexit
    APP.initQgis()
    # Native capture tools initialize registries which require orderly QGIS
    # shutdown before Python destroys their Qt objects and module globals.
    atexit.register(APP.exitQgis)
# Never change the real user's application settings during headless tests.
SETTINGS_DIR = tempfile.TemporaryDirectory()
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, SETTINGS_DIR.name)
APP.setOrganizationName("AcalcTests")
APP.setApplicationName("AcalcTests")


def rows_for(kind):
    rows = [(0, 500000.125, 4400000.5, 1500.25, 1.5)]
    if kind.geometry != "Point":
        rows.append((0, 500020.25, 4400000.75, 1501.0, 2.75))
    if kind.geometry == "Polygon":
        rows.extend([(0, 500020.5, 4400020.25, 1502.5, 4.0), (0, 500000.125, 4400020.5, 1501.5, 5.0)])
    return rows


def add_feature(layer, kind, name="Example"):
    if not layer.isEditable():
        assert layer.startEditing()
    feature = QgsFeature(layer.fields())
    feature["object_id"] = new_id()
    feature["name"] = name
    feature["notes"] = "Test feature"
    feature["enabled"] = 1
    if kind.key == "ground_absorption":
        feature["ground_factor"] = 0.5
    if kind.key == "trees":
        feature["canopy_height"] = 10.0
    if kind.key == "area_receiver":
        feature["height_m"], feature["spacing_m"] = 1.5, 10.0
    feature.setGeometry(geometry_from_rows(rows_for(kind), kind))
    assert layer.addFeature(feature)
    return feature


class FakeIface(QObject):
    """Only the QGIS application shell is substituted; layers/canvas/dock are real."""
    currentLayerChanged = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.window = QMainWindow()
        self.window.resize(1150, 900)
        self.canvas = QgsMapCanvas(self.window)
        self.window.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar(self.window)
        self.active = None
        self.add_action = QAction(self.window)
        self.cad = None

    def mainWindow(self): return self.window
    def mapCanvas(self): return self.canvas
    def messageBar(self): return self.bar
    def activeLayer(self): return self.active
    def actionAddFeature(self): return self.add_action
    def cadDockWidget(self):
        if self.cad is None:
            self.cad = QgsAdvancedDigitizingDockWidget(self.canvas, self.window)
            self.cad.hide()
        return self.cad
    def addPluginToMenu(self, *args): pass
    def removePluginMenu(self, *args): pass
    def addToolBarIcon(self, *args): pass
    def removeToolBarIcon(self, *args): pass
    def addDockWidget(self, area, dock): self.window.addDockWidget(area, dock)
    def removeDockWidget(self, dock): self.window.removeDockWidget(dock)

    def setActiveLayer(self, layer):
        self.active = layer
        self.currentLayerChanged.emit(layer)


class ModelFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "model #1.gpkg"
        self.store = ModelStore.create(self.path, "Acalc test", QgsCoordinateReferenceSystem("EPSG:26913").toWkt(), "EPSG:26913", "Test author")

    def tearDown(self):
        QgsProject.instance().clear()
        for task in APP.taskManager().tasks():
            task.waitForFinished(3000)
        APP.processEvents()
        gc.collect()
        self.directory.cleanup()


class StoreTests(ModelFixture):
    def test_metadata_persistence_and_no_overwrite(self):
        metadata = self.store.metadata()
        self.store.update_metadata("Updated", "Author", "Description", "NAVD88", {"client": "A & B"})
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.metadata()["project_id"], metadata["project_id"])
        self.assertEqual(reopened.metadata()["name"], "Updated")
        self.assertEqual(json.loads(reopened.metadata()["metadata_json"]), {"client": "A & B"})
        with self.assertRaises(ModelError):
            ModelStore.create(self.path, "Overwrite", metadata["crs_wkt"])
        self.assertEqual(reopened.metadata()["name"], "Updated")

    def test_all_geometry_types_roundtrip_with_varying_z_and_height(self):
        for kind in LAYER_TYPES.values():
            with self.subTest(kind=kind.key):
                record = self.store.add_layer(kind.key, kind.title)
                layer = load_layer(self.store, record)
                feature = add_feature(layer, kind)
                expected = vertex_rows(feature.geometry(), kind)
                self.assertEqual(validate_layer(layer, kind), [])
                self.assertTrue(layer.commitChanges(), layer.commitErrors())
                del layer
                reopened = load_layer(ModelStore.open(self.path), record)
                loaded = next(reopened.getFeatures())
                self.assertEqual(vertex_rows(loaded.geometry(), kind), expected)
                self.assertEqual(loaded["name"], "Example")
                self.assertIsNotNone(reopened.renderer())
                if kind.geometry == "Polygon":
                    self.assertEqual(loaded.geometry().constGet().numInteriorRings(), 0)

    def test_variants_membership_rename_archive_restore(self):
        source = self.store.add_layer("point_source", "Sources")
        receiver = self.store.add_layer("point_receiver", "Receivers")
        config = self.store.variants()[0]
        self.assertEqual(config["definition"]["layer_ids"], [])
        both = definition([source["layer_id"], receiver["layer_id"]])
        self.store.save_variant(config["variant_id"], "Existing", both)
        future = self.store.add_variant("Future", definition([receiver["layer_id"]]))
        self.store.rename_layer(source["layer_id"], "Renamed sources")
        self.assertEqual(self.store.variants()[0]["definition"], both)
        self.store.archive_layer(receiver["layer_id"])
        for cfg in self.store.variants():
            self.assertNotIn(receiver["layer_id"], cfg["definition"]["layer_ids"])
        self.store.restore_layer(receiver["layer_id"])
        self.assertEqual(len(ModelStore.open(self.path).layers()), 2)
        self.assertEqual(self.store.export_variant(future)["layers"], [])
        self.store.delete_variant(future)
        with self.assertRaises(ModelError):
            self.store.delete_variant(config["variant_id"])
        with self.assertRaises(ModelError):
            self.store.add_variant("existing")
        with self.assertRaises(ModelError):
            self.store.add_variant("Dangling", definition(["unknown"]))

    def test_reject_foreign_and_future_files_without_modifying_them(self):
        foreign = Path(self.directory.name) / "foreign.gpkg"
        from contextlib import closing
        with closing(sqlite3.connect(foreign)) as db:
            db.execute("CREATE TABLE hello(value TEXT)")
        with self.assertRaises(ModelError):
            ModelStore.open(foreign)
        with self.store.connection() as db:
            db.execute("UPDATE acalc_project SET schema_version=999")
        with self.assertRaisesRegex(ModelError, "Unsupported"):
            ModelStore.open(self.path)

    def test_coordinate_validation(self):
        kind = LAYER_TYPES["point_source"]
        for bad in ([(0, 1, 2, math.nan, 1)], [(0, 1, 2, 3, -1)], [(0, math.inf, 2, 3, 1)]):
            with self.assertRaises(ModelError):
                geometry_from_rows(bad, kind)
        with self.assertRaises(ModelError):
            geometry_from_rows([(0, 0, 0, 0, 1)], LAYER_TYPES["barrier"])
        with self.assertRaises(ModelError):
            geometry_from_rows([(0, 0, 0, 0, 1), (0, 1, 1, 0, 1), (0, 0, 1, 0, 1), (0, 1, 0, 0, 1)], LAYER_TYPES["building"])
        with self.assertRaises(ModelError):
            ModelStore.create(Path(self.directory.name) / "geographic.gpkg", "Bad CRS", QgsCoordinateReferenceSystem("EPSG:4326").toWkt())


class PluginTests(ModelFixture):
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

    def test_open_group_bidirectional_selection_and_layer_reload(self):
        source = self.store.add_layer("point_source", "Sources")
        terrain = self.store.add_layer("terrain_line", "Contours")
        unrelated = QgsVectorLayer("Point?crs=EPSG:26913", "Reference", "memory")
        QgsProject.instance().addMapLayer(unrelated)
        self.assertTrue(self.plugin.open_project(self.path))
        self.assertEqual(len(self.plugin.group.findLayers()), 2)
        layer = self.plugin.layers[source["layer_id"]]
        feature = add_feature(layer, LAYER_TYPES["point_source"], "Pump")
        self.plugin.commit_layer(layer)
        feature = next(layer.getFeatures())
        self.iface.setActiveLayer(layer)
        layer.selectByIds([feature.id()])
        APP.processEvents()
        dock = self.plugin.dock
        self.assertEqual(dock.current_kind, "point_source")
        self.assertEqual(dock.features.selectedItems()[0].data(Qt.UserRole), feature.id())
        dock.features.clearSelection()
        self.assertEqual(layer.selectedFeatureCount(), 0)
        dock.features.selectRow(0)
        self.assertEqual(layer.selectedFeatureIds(), [feature.id()])
        dock.show_view("terrain_line")
        self.assertIs(self.iface.activeLayer(), self.plugin.layers[terrain["layer_id"]])
        QgsProject.instance().removeMapLayer(layer.id())
        self.assertNotIn(source["layer_id"], self.plugin.layers)
        self.plugin.reload_layers()
        self.assertEqual(len(self.plugin.group.findLayers()), 2)
        self.assertTrue(self.plugin.close_project())
        self.assertEqual(list(QgsProject.instance().mapLayers()), [unrelated.id()])

    def test_model_styles_survive_adoption_reload_and_project_restore(self):
        from qgis.PyQt.QtGui import QColor
        from qgis.core import QgsPalLayerSettings, QgsVectorLayerSimpleLabeling

        for kind in LAYER_TYPES.values():
            record = self.store.add_layer(kind.key, kind.title)
            layer = load_layer(self.store, record)
            layer.renderer().symbol().setColor(QColor("#c13570"))
            layer.setOpacity(0.37)
            labels = QgsPalLayerSettings()
            labels.fieldName = "notes"
            layer.setLabeling(QgsVectorLayerSimpleLabeling(labels))
            layer.setLabelsEnabled(kind.key != "measurement")
            layer.setDisplayExpression('"notes"')
            QgsProject.instance().addMapLayer(layer)

        def check():
            for layer in self.plugin.layers.values():
                with self.subTest(layer=layer.name()):
                    self.assertEqual(layer.renderer().symbol().color().name(), "#c13570")
                    self.assertAlmostEqual(layer.opacity(), 0.37)
                    self.assertEqual(layer.labeling().settings().fieldName, "notes")
                    self.assertEqual(layer.labelsEnabled(), layer.customProperty("acalc/feature_type") != "measurement")
                    self.assertEqual(layer.displayExpression(), '"notes"')

        self.plugin.open_project(self.path)
        check()
        self.plugin.reload_layers()
        check()
        self.plugin.unload()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        check()
        project_path = str(Path(self.directory.name) / "custom styles.qgs")
        self.assertTrue(QgsProject.instance().write(project_path))
        QgsProject.instance().clear()
        self.assertTrue(QgsProject.instance().read(project_path))
        check()

    def test_feature_dialog_apply_undo_commit_and_reopen(self):
        record = self.store.add_layer("barrier", "Noise wall")
        self.plugin.open_project(self.path)
        self.plugin.dock.show_view("barrier")
        layer = self.plugin.selected_layer()

        def fill_dialog(dialog):
            dialog.name.setText("Wall A")
            dialog.table.setRowCount(0)
            for row in rows_for(LAYER_TYPES["barrier"]):
                dialog.add_values(row)
            dialog.validate()
            return dialog.result()

        with patch.object(FeatureDialog, "exec", fill_dialog):
            self.plugin.edit_feature(True)
        self.assertEqual(layer.featureCount(), 1)
        self.assertTrue(layer.isModified())
        layer.undoStack().undo()
        self.assertEqual(layer.featureCount(), 0)
        layer.undoStack().redo()
        self.assertEqual(layer.featureCount(), 1)
        self.plugin.save_layer()
        self.assertFalse(layer.isEditable())
        self.plugin.close_project()
        self.plugin.open_project(self.path)
        loaded = next(self.plugin.layers[record["layer_id"]].getFeatures())
        self.assertEqual(loaded["name"], "Wall A")
        self.assertEqual([p.m() for p in loaded.geometry().vertices()], [1.5, 2.75])

    def test_editing_prevents_silent_project_close_and_invalid_save(self):
        record = self.store.add_layer("point_receiver", "Receivers")
        self.plugin.open_project(self.path)
        layer = self.plugin.layers[record["layer_id"]]
        feature = add_feature(layer, LAYER_TYPES["point_receiver"])
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel):
            self.assertFalse(self.plugin.close_project())
        self.assertTrue(layer.isModified())
        layer.changeGeometry(feature.id(), QgsGeometry.fromWkt("POINT ZM (1 2 3 -1)"))
        with self.assertRaises(ModelError):
            self.plugin.commit_layer(layer)
        self.assertTrue(layer.isModified())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Discard):
            self.assertTrue(self.plugin.close_project())

    def test_config_dock_membership_and_saved_qgis_project_reuse(self):
        record = self.store.add_layer("building", "Buildings")
        self.plugin.open_project(self.path)
        self.plugin.dock.show_view("project")
        next(self.plugin.dock.member_items()).setCheckState(0, Qt.Checked)
        self.assertEqual(self.store.variants()[0]["definition"]["layer_ids"], [record["layer_id"]])
        qgs = str(Path(self.directory.name) / "map.qgz")
        self.assertTrue(QgsProject.instance().write(qgs))
        self.plugin.unload()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.assertEqual(len(self.plugin.group.findLayers()), 1)
        self.assertEqual(len(QgsProject.instance().mapLayers()), 1)
        self.assertTrue(QgsProject.instance().read(qgs))
        APP.processEvents()
        self.assertEqual(len(self.plugin.layers), 1)
        self.assertEqual(len(self.plugin.group.findLayers()), 1)

    def test_native_selection_returns_from_main_and_menu_actions_work(self):
        record = self.store.add_layer("point_source", "Plant")
        self.plugin.open_project(self.path)
        layer = self.plugin.layers[record["layer_id"]]
        self.iface.setActiveLayer(layer)
        # Exercise the same defaults QGIS's digitizing form uses.
        feature = QgsVectorLayerUtils.createFeature(layer, geometry_from_rows(rows_for(LAYER_TYPES["point_source"]), LAYER_TYPES["point_source"]))
        layer.startEditing()
        self.assertTrue(layer.addFeature(feature))
        self.assertEqual(validate_layer(layer, LAYER_TYPES["point_source"]), [])
        self.plugin.commit_layer(layer)
        self.plugin.dock.show_view("project")
        layer.selectByIds([next(layer.getFeatures()).id()])
        APP.processEvents()
        self.assertIs(self.plugin.dock.stack.currentWidget(), self.plugin.dock.sources_page)
        self.assertEqual(self.plugin.dock.current_kind, "point_source")
        from qgis.PyQt.QtWidgets import QToolButton
        menu = self.plugin.dock.findChild(QToolButton).menu()
        next(action for action in menu.actions() if action.text() == "Objects").trigger()
        self.assertEqual(self.plugin.dock.current_kind, "building")

    def test_shared_geopackage_edit_buffers_and_external_layer_adoption(self):
        from osgeo import gdal
        issues = []
        gdal.PushErrorHandler(lambda level, number, message: issues.append(message) if level >= gdal.CE_Failure else None)
        try:
            a = self.store.add_layer("point_source", "Plant")
            b = self.store.add_layer("point_receiver", "Receivers")
            self.plugin.open_project(self.path)
            source, receiver = self.plugin.layers[a["layer_id"]], self.plugin.layers[b["layer_id"]]
            add_feature(source, LAYER_TYPES["point_source"])
            add_feature(receiver, LAYER_TYPES["point_receiver"])
            self.plugin.dock.show_view("terrain_point")
            with patch.object(QInputDialog, "getText", return_value=("New terrain", True)):
                self.plugin.add_layer()
            self.plugin.store.update_metadata("Changed", "Author", "Notes", "Unspecified")
            self.plugin.commit_layer(source)
            self.assertTrue(receiver.isModified())
            self.plugin.commit_layer(receiver)
            self.assertEqual(source.featureCount(), 1)
            self.assertEqual(receiver.featureCount(), 1)
            self.assertEqual(len(self.plugin.layers), 3)
            QgsProject.instance().removeMapLayer(source.id())
            external = load_layer(self.store, a)
            QgsProject.instance().addMapLayer(external)
            APP.processEvents()
            self.assertIs(self.plugin.layers[a["layer_id"]], external)
            nodes = [node for node in QgsProject.instance().layerTreeRoot().findLayers() if node.layerId() == external.id()]
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0].parent().name(), "Sources")
            self.assertIs(nodes[0].parent().parent(), self.plugin.group)
            self.assertEqual(issues, [])
        finally:
            gdal.PopErrorHandler()


if __name__ == "__main__":
    unittest.main()
