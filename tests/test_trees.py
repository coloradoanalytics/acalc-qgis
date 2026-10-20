# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Trees authoring, SI height storage, and XY import regressions in QGIS."""

from unittest.mock import patch

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import QDialog, QDoubleSpinBox
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsFeature, QgsField, QgsGeometry,
    QgsProject, QgsVectorLayer, QgsVectorLayerUtils, QgsWkbTypes,
)

from acalc.dialogs import FeatureDialog
from acalc.layer_import import ImportOptions, import_layer
from acalc.layer_import_dialog import ImportLayerDialog
from acalc.layers import geometry_from_rows, load_layer, validate_geometry, validate_layer
from acalc.plugin import AcalcPlugin
from acalc.project_coordinates import change_coordinates
from acalc.schema import LAYER_TYPES, ModelError, definition
from acalc.storage import ModelStore
from tests.test_model import APP, FakeIface, ModelFixture, add_feature, rows_for


KIND = LAYER_TYPES["trees"]


class TreesTests(ModelFixture):
    def test_editor_units_undo_cancel_save_and_variant_roundtrip(self):
        record = self.store.add_layer("trees", "Woodland")
        variant = self.store.variants()[0]
        self.store.save_variant(variant["variant_id"], variant["name"], definition([record["layer_id"]]))
        change_coordinates(self.store, QgsCoordinateReferenceSystem("EPSG:26913"), "ft",
                           QgsProject.instance().transformContext())
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        try:
            plugin.open_project(self.path)
            plugin.dock.show_view("trees")
            self.assertEqual(plugin.dock.heading.text(), "Terrain")
            self.assertFalse(plugin.dock.set_z_values_button.isVisible())
            layer = plugin.layers[record["layer_id"]]
            self.assertEqual(plugin.project.layerTreeRoot().findLayer(layer.id()).parent().name(), "Objects")
            default = QgsVectorLayerUtils.createFeature(layer)
            self.assertEqual(default["canopy_height"], 10)

            def create(dialog):
                self.assertEqual(dialog.columns, ["X", "Y"])
                self.assertFalse(hasattr(dialog, "ground_mode"))
                self.assertFalse(hasattr(dialog, "raster_button"))
                self.assertFalse(dialog.notes.isHidden())
                self.assertEqual(dialog.canopy_height.suffix(), " ft")
                self.assertAlmostEqual(QDoubleSpinBox.value(dialog.canopy_height), 10 / .3048, places=6)
                for phrase in ("blocks the view", "Annex A.2.2", "Acalc default", "ground absorption separately"):
                    self.assertIn(phrase, dialog.trees_guidance.toolTip())
                QDoubleSpinBox.setValue(dialog.canopy_height, 50)
                dialog.name.setText("Dense woodland")
                dialog.notes.setPlainText("Dense evergreen planting; surveyed height")
                dialog.table.setRowCount(0)
                for row in rows_for(KIND):
                    dialog.add_values(row)
                dialog.validate()
                self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
                return dialog.result()

            with patch.object(FeatureDialog, "exec", create):
                self.assertTrue(plugin.edit_feature(create=True, layer=layer))
            feature = next(layer.getFeatures())
            fid = feature.id()
            self.assertEqual(feature.geometry().wkbType(), QgsWkbTypes.Polygon)
            self.assertAlmostEqual(feature["canopy_height"], 15.24)
            self.assertEqual(validate_layer(layer, KIND), [])
            layer.undoStack().undo()
            self.assertEqual(layer.featureCount(), 0)
            layer.undoStack().redo()

            def edit(dialog):
                self.assertAlmostEqual(dialog.canopy_height.value(), 15.24)
                dialog.canopy_height.setValue(18)
                dialog.validate()
                return dialog.result()

            with patch.object(FeatureDialog, "exec", edit):
                self.assertTrue(plugin.edit_feature(layer=layer, feature_id=fid))
            self.assertEqual(layer.getFeature(fid)["canopy_height"], 18)
            layer.undoStack().undo()
            self.assertAlmostEqual(layer.getFeature(fid)["canopy_height"], 15.24)
            layer.undoStack().redo()

            def cancel(dialog):
                dialog.canopy_height.setValue(5)
                return QDialog.Rejected

            with patch.object(FeatureDialog, "exec", cancel):
                self.assertFalse(plugin.edit_feature(layer=layer, feature_id=fid))
            self.assertEqual(layer.getFeature(fid)["canopy_height"], 18)
            plugin.commit_layer(layer)
            reopened_store = ModelStore.open(self.path)
            reopened = load_layer(reopened_store, record)
            saved = next(reopened.getFeatures())
            self.assertEqual((saved["name"], saved["canopy_height"]), ("Dense woodland", 18))
            self.assertEqual(saved["notes"], "Dense evergreen planting; surveyed height")
            self.assertEqual(reopened_store.variants()[0]["definition"]["layer_ids"], [record["layer_id"]])
            self.assertFalse({"infer_z", "ground_mode", "ground_factor"} & set(reopened.fields().names()))
            with self.store.connection(readonly=True) as db:
                dimensions = db.execute("SELECT z,m FROM gpkg_geometry_columns WHERE table_name=?", (record["table_name"],)).fetchone()
                self.assertEqual(tuple(dimensions), (0, 0))
        finally:
            plugin.unload()
            iface.window.close()
            APP.processEvents()

    def test_height_validation_and_required_field(self):
        record = self.store.add_layer("trees", "Woodland")
        layer = load_layer(self.store, record)
        feature = add_feature(layer, KIND)
        index = layer.fields().indexFromName("canopy_height")
        for value in (None, 0, -1, float("inf"), float("nan"), "invalid"):
            with self.subTest(value=value):
                layer.changeAttributeValue(feature.id(), index, value)
                self.assertIn("Tree height", validate_layer(layer, KIND)[0])
        layer.changeAttributeValue(feature.id(), index, 12.5)
        self.assertEqual(validate_layer(layer, KIND), [])
        self.assertTrue(layer.commitChanges(), layer.commitErrors())
        del feature, layer
        with self.store.connection() as db:
            db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN canopy_height')
        with self.assertRaisesRegex(ModelError, "Missing required fields"):
            ModelStore.open(self.path)

    def test_geometry_uses_only_xy_and_rejects_holes_and_crossings(self):
        geometry = geometry_from_rows([(ring, x, y, float("nan"), float("nan"))
                                       for ring, x, y, _, _ in rows_for(KIND)], KIND)
        self.assertEqual(geometry.wkbType(), QgsWkbTypes.Polygon)
        validate_geometry(geometry, KIND)
        for wkt in ("POLYGON Z ((0 0 0,10 0 0,0 10 0,0 0 0))",
                    "POLYGON ((0 0,10 10,0 10,10 0,0 0))",
                    "POLYGON ((0 0,10 0,10 10,0 10,0 0),(2 2,3 2,3 3,2 2))"):
            with self.subTest(wkt=wkt), self.assertRaises(ModelError):
                validate_geometry(QgsGeometry.fromWkt(wkt), KIND)

    def source(self):
        source = QgsVectorLayer("PolygonZM?crs=EPSG:26913", "Survey woodland", "memory")
        source.dataProvider().addAttributes([QgsField("height_ft", QVariant.Double)])
        source.updateFields()
        feature = QgsFeature(source.fields())
        feature["height_ft"] = 50
        feature.setGeometry(QgsGeometry.fromWkt("POLYGON ZM ((0 0 900 100,10 0 910 200,0 10 920 300,0 0 900 100))"))
        self.assertTrue(source.dataProvider().addFeatures([feature])[0])
        QgsProject.instance().addMapLayer(source)
        return source

    def test_import_tree_height_units_ignores_ground_z_and_rejects_invalid_values(self):
        source = self.source()
        record, _, _ = import_layer(self.store, source, ImportOptions(
            "trees", "Woodland", canopy_height_field="height_ft", vertical_to_m=.3048, ground_mode="geometry_z"))
        layer = load_layer(self.store, record)
        feature = next(layer.getFeatures())
        self.assertEqual(feature.geometry().wkbType(), QgsWkbTypes.Polygon)
        self.assertAlmostEqual(feature["canopy_height"], 15.24)
        self.assertEqual(validate_layer(layer, KIND), [])
        self.assertFalse(KIND.ground_points)
        for height in (0, -1, float("inf"), float("nan")):
            with self.subTest(height=height), self.assertRaisesRegex(ModelError, "Tree height"):
                import_layer(self.store, source, ImportOptions("trees", "Invalid", canopy_height=height))
        self.assertEqual(len(self.store.layers()), 1)

    def test_import_dialog_shows_tree_height_without_ground_controls(self):
        source = self.source()
        dialog = ImportLayerDialog(None, self.store, active_layer=source, kind_key="trees")
        try:
            self.assertEqual(dialog.kind.currentData(), "trees")
            self.assertFalse(dialog.canopy_height.isHidden())
            self.assertFalse(dialog.trees_guidance.isHidden())
            self.assertTrue(dialog.ground_mode.isHidden())
            self.assertTrue(dialog.height.isHidden())
            self.assertTrue(dialog.ground_factor.isHidden())
            self.assertEqual(dialog.options().canopy_height, 10)
            dialog.canopy_height.field.setCurrentIndex(dialog.canopy_height.field.findData("height_ft"))
            dialog.source_units.setCurrentIndex(dialog.source_units.findData("ft"))
            self.assertEqual(dialog.options().canopy_height_field, "height_ft")
            self.assertEqual(dialog.options().vertical_to_m, .3048)
        finally:
            dialog.close()
