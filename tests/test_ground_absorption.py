# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""2D ground absorption authoring with real QGIS layers and GeoPackages."""

from unittest.mock import patch

from qgis.PyQt.QtWidgets import QDialog
from qgis.core import QgsGeometry, QgsWkbTypes

from acalc.dialogs import FeatureDialog
from acalc.layers import geometry_from_rows, ground_absorption_overlap_errors, load_layer, validate_geometry, validate_layer
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, ModelError
from acalc.storage import ModelStore
from tests.test_model import APP, FakeIface, ModelFixture, add_feature, rows_for


KIND = LAYER_TYPES["ground_absorption"]


class GroundAbsorptionTests(ModelFixture):
    def test_overlap_detection_rejects_intersections_containment_and_duplicates(self):
        record = self.store.add_layer(KIND.key, "Lawns")
        layer = load_layer(self.store, record)
        first = add_feature(layer, KIND, "North lawn")
        layer.changeGeometry(first.id(), QgsGeometry.fromWkt("POLYGON ((0 0,10 0,10 10,0 10,0 0))"))
        second = add_feature(layer, KIND, "South lawn")
        for wkt in (
            "POLYGON ((5 5,15 5,15 15,5 15,5 5))",  # intersecting interiors
            "POLYGON ((2 2,8 2,8 8,2 8,2 2))",  # contained polygon
            "POLYGON ((0 0,10 0,10 10,0 10,0 0))",  # duplicate footprint
            "POLYGON ((0 0,0 10,10 10,10 0,0 0))",  # reverse winding
            "POLYGON ((9.999999 0,20 0,20 10,9.999999 10,9.999999 0))",  # narrow positive area
        ):
            with self.subTest(wkt=wkt):
                layer.changeGeometry(second.id(), QgsGeometry.fromWkt(wkt))
                errors = ground_absorption_overlap_errors([layer])
                self.assertEqual(len(errors), 1)
                for label in ("overlap", "North lawn", "South lawn", "Lawns", str(first.id()), str(second.id())):
                    self.assertIn(label, errors[0])
        layer.rollBack()

    def test_overlap_detection_allows_shared_edges_vertices_and_disjoint_interiors(self):
        record = self.store.add_layer(KIND.key, "Ground")
        layer = load_layer(self.store, record)
        first = add_feature(layer, KIND, "L-shaped lawn")
        layer.changeGeometry(first.id(), QgsGeometry.fromWkt("POLYGON ((0 0,10 0,10 3,3 3,3 10,0 10,0 0))"))
        second = add_feature(layer, KIND, "Paving")
        for wkt in (
            "POLYGON ((10 0,20 0,20 3,10 3,10 0))",  # shared edge
            "POLYGON ((10 3,20 3,20 10,10 10,10 3))",  # shared vertex
            "POLYGON ((5 5,8 5,8 8,5 8,5 5))",  # overlapping bounding boxes only
            "POLYGON ((20 20,30 20,30 30,20 30,20 20))",  # disjoint bounds
        ):
            with self.subTest(wkt=wkt):
                layer.changeGeometry(second.id(), QgsGeometry.fromWkt(wkt))
                self.assertEqual(ground_absorption_overlap_errors([layer]), [])
        layer.rollBack()

    def test_project_validation_alerts_for_overlap_across_layers_and_respects_explicit_scope(self):
        records = [self.store.add_layer(KIND.key, name) for name in ("Lawns", "Paved surfaces")]
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        try:
            plugin.open_project(self.path)
            for record, name in zip(records, ("Grass", "Car park")):
                add_feature(plugin.layers[record["layer_id"]], KIND, name)
            errors = plugin.validation_errors()
            self.assertEqual(len(errors), 1)
            for label in ("Lawns", "Paved surfaces", "Grass", "Car park"):
                self.assertIn(label, errors[0])
            self.assertEqual(plugin.validation_errors([records[0]["layer_id"]]), [])
            alerts = []
            with patch("acalc.plugin.QMessageBox.exec", lambda box: alerts.append(box.detailedText())):
                plugin.show_validation()
            self.assertEqual(alerts, [errors[0]])
            self.assertIn("overlap", plugin.dock.status.text())
            # Invalid geometries are handled by normal validation without GEOS
            # overlap errors obscuring the actual perimeter error.
            layer = plugin.layers[records[1]["layer_id"]]
            layer.changeGeometry(next(layer.getFeatures()).id(), QgsGeometry.fromWkt("POLYGON ((0 0,10 10,0 10,10 0,0 0))"))
            self.assertIn("perimeter is invalid", plugin.validation_errors()[0])
        finally:
            for layer in plugin.layers.values():
                if layer.isEditable():
                    layer.rollBack()
            plugin.unload()
            iface.window.close()
            APP.processEvents()

    def test_properties_create_edit_cancel_undo_save_reopen(self):
        record = self.store.add_layer(KIND.key, "Ground")
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        try:
            plugin.open_project(self.path)
            plugin.dock.show_view(KIND.key)
            self.assertEqual(plugin.dock.heading.text(), "Terrain")
            layer = plugin.layers[record["layer_id"]]

            def create(dialog):
                self.assertEqual(dialog.columns, ["X", "Y"])
                self.assertFalse(hasattr(dialog, "ground_mode"))
                self.assertFalse(hasattr(dialog, "raster_button"))
                self.assertTrue(dialog.notes.isHidden())
                self.assertEqual(dialog.ground_factor.value(), 0)
                self.assertEqual((dialog.ground_factor.minimum(), dialog.ground_factor.maximum()), (0, 1))
                dialog.name.setText("Lawn")
                dialog.ground_factor.setValue(0.65)
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
            self.assertEqual((feature["name"], feature["ground_factor"]), ("Lawn", 0.65))
            self.assertEqual(validate_layer(layer, KIND), [])
            layer.undoStack().undo()
            self.assertEqual(layer.featureCount(), 0)
            layer.undoStack().redo()

            def edit(dialog):
                self.assertEqual(dialog.ground_factor.value(), 0.65)
                dialog.ground_factor.setValue(1)
                dialog.validate()
                return dialog.result()

            with patch.object(FeatureDialog, "exec", edit):
                self.assertTrue(plugin.edit_feature(layer=layer, feature_id=fid))
            self.assertEqual(layer.getFeature(fid)["ground_factor"], 1)
            layer.undoStack().undo()
            self.assertEqual(layer.getFeature(fid)["ground_factor"], 0.65)
            layer.undoStack().redo()

            def cancel(dialog):
                dialog.ground_factor.setValue(0)
                return QDialog.Rejected

            with patch.object(FeatureDialog, "exec", cancel):
                self.assertFalse(plugin.edit_feature(layer=layer, feature_id=fid))
            self.assertEqual(layer.getFeature(fid)["ground_factor"], 1)
            plugin.commit_layer(layer)
            reopened = load_layer(ModelStore.open(self.path), record)
            saved = next(reopened.getFeatures())
            self.assertEqual((saved["name"], saved["ground_factor"]), ("Lawn", 1))
            self.assertEqual(saved.geometry().wkbType(), QgsWkbTypes.Polygon)
            self.assertNotIn("infer_z", reopened.fields().names())
            self.assertNotIn("ground_mode", reopened.fields().names())
            with self.store.connection(readonly=True) as db:
                dimensions = db.execute("SELECT z,m FROM gpkg_geometry_columns WHERE table_name=?", (record["table_name"],)).fetchone()
                self.assertEqual(tuple(dimensions), (0, 0))
        finally:
            plugin.unload()
            iface.window.close()
            APP.processEvents()

    def test_g_range_and_required_field_validation(self):
        record = self.store.add_layer(KIND.key, "Ground")
        layer = load_layer(self.store, record)
        feature = add_feature(layer, KIND)
        index = layer.fields().indexFromName("ground_factor")
        for value in (None, -0.1, 1.1, float("inf"), float("nan"), "invalid"):
            with self.subTest(value=value):
                layer.changeAttributeValue(feature.id(), index, value)
                self.assertTrue(validate_layer(layer, KIND))
        for value in (0, 0.375, 1):
            layer.changeAttributeValue(feature.id(), index, value)
            self.assertEqual(validate_layer(layer, KIND), [])
        self.assertTrue(layer.commitChanges(), layer.commitErrors())
        del feature, layer
        with self.store.connection() as db:
            db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN ground_factor')
        with self.assertRaisesRegex(ModelError, "Missing required fields"):
            ModelStore.open(self.path)

    def test_polygon_requires_only_xy_and_rejects_invalid_geometry(self):
        rows = [(ring, x, y, float("nan"), float("nan")) for ring, x, y, _, _ in rows_for(KIND)]
        geometry = geometry_from_rows(rows, KIND)
        self.assertEqual(geometry.wkbType(), QgsWkbTypes.Polygon)
        validate_geometry(geometry, KIND)
        for wkt in ("POLYGON Z ((0 0 0,10 0 0,0 10 0,0 0 0))",
                    "POLYGON ((0 0,10 10,0 10,10 0,0 0))"):
            with self.assertRaises(ModelError):
                validate_geometry(QgsGeometry.fromWkt(wkt), KIND)
