# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Real GeoPackage reprojection and project input-unit regressions."""

import json
from unittest.mock import patch

from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry, QgsProject
from qgis.PyQt.QtWidgets import QDialog, QTableWidgetItem

from acalc.dialogs import FeatureDialog, ProjectCoordinatesDialog
from acalc.layers import load_layer, vertex_rows
from acalc.plugin import AcalcPlugin
from acalc.project_coordinates import change_coordinates
from acalc.schema import LAYER_TYPES, ModelError
from acalc.storage import ModelStore
from acalc.units import LengthSpinBox, input_unit, elevation_unit, length_factor
from tests.test_model import APP, FakeIface, ModelFixture, add_feature


class ProjectCoordinatesTests(ModelFixture):
    def feet_crs(self):
        return QgsCoordinateReferenceSystem.fromProj('+proj=utm +zone=13 +datum=NAD83 +units=ft +type=crs')

    def change(self, crs, unit='ft'):
        return change_coordinates(self.store, crs, unit, QgsProject.instance().transformContext())

    def test_reprojects_every_kind_and_archives_preserving_attributes_z_m(self):
        originals = []
        for kind in LAYER_TYPES.values():
            record = self.store.add_layer(kind.key, kind.title)
            layer = load_layer(self.store, record)
            add_feature(layer, kind)
            self.assertTrue(layer.commitChanges())
            feature = next(layer.getFeatures())
            originals.append((record, kind, vertex_rows(feature.geometry(), kind), feature.attributes()))
            del layer
        with self.store.connection() as db:
            db.execute('UPDATE acalc_layers SET archived=1 WHERE layer_id=?', (originals[-1][0]['layer_id'],))
        self.assertTrue(self.change(self.feet_crs()))
        ModelStore.open(self.path)
        self.assertEqual(elevation_unit(self.store), 'ft')
        self.assertAlmostEqual(self.store.metadata()['horizontal_to_m'], .3048)
        for record, kind, original, attributes in originals:
            layer = load_layer(self.store, record)
            feature = next(layer.getFeatures())
            self.assertEqual(feature.attributes(), attributes)
            for before, after in zip(original, vertex_rows(feature.geometry(), kind)):
                self.assertAlmostEqual(after[1], before[1]/.3048, places=6)
                self.assertAlmostEqual(after[2], before[2]/.3048, places=6)
                self.assertEqual(after[3:], before[3:])
        self.change(QgsCoordinateReferenceSystem('EPSG:26913'), 'm')
        ModelStore.open(self.path)
        for record, kind, original, _ in originals:
            feature = next(load_layer(self.store, record).getFeatures())
            for before, after in zip(original, vertex_rows(feature.geometry(), kind)):
                self.assertAlmostEqual(after[1], before[1], places=6)
                self.assertAlmostEqual(after[2], before[2], places=6)

    def test_failed_transform_rolls_back_geometry_registry_and_metadata(self):
        for _ in range(2):
            record = self.store.add_layer('point_source', 'Source')
            layer = load_layer(self.store, record)
            add_feature(layer, LAYER_TYPES['point_source'])
            self.assertTrue(layer.commitChanges())
        before = self.store.metadata()
        with self.store.connection(readonly=True) as db:
            geometry = {r['table_name']: db.execute(f'SELECT geom FROM "{r["table_name"]}"').fetchone()[0] for r in self.store.layers()}
            srs = list(db.execute('SELECT * FROM gpkg_geometry_columns'))
        original = QgsGeometry.transform
        count = 0
        def fail_second(obj, *args):
            nonlocal count
            count += 1
            if count == 2:
                raise ModelError('Test transformation failure')
            return original(obj, *args)
        with patch.object(QgsGeometry, 'transform', fail_second):
            with self.assertRaisesRegex(ModelError, 'Test transformation failure'):
                self.change(self.feet_crs())
        self.assertEqual(self.store.metadata(), before)
        ModelStore.open(self.path)
        with self.store.connection(readonly=True) as db:
            self.assertEqual(list(db.execute('SELECT * FROM gpkg_geometry_columns')), srs)
            for table, blob in geometry.items():
                self.assertEqual(db.execute(f'SELECT geom FROM "{table}"').fetchone()[0], blob)

    def test_units_only_preserves_geometry_and_rejects_geographic_crs(self):
        record = self.store.add_layer('point_receiver', 'Receiver')
        layer = load_layer(self.store, record)
        add_feature(layer, LAYER_TYPES['point_receiver'])
        layer.commitChanges()
        blob = bytes(next(layer.getFeatures()).geometry().asWkb())
        self.change(layer.crs())
        self.assertEqual(bytes(next(load_layer(self.store, record).getFeatures()).geometry().asWkb()), blob)
        self.assertFalse(self.change(layer.crs()))
        with self.assertRaisesRegex(ModelError, 'projected'):
            self.change(QgsCoordinateReferenceSystem('EPSG:4326'))
        self.assertEqual(elevation_unit(ModelStore.open(self.path)), 'ft')

    def test_elevation_feet_preserves_crs_xy_and_converts_z_height_and_raster_values(self):
        kind = LAYER_TYPES['point_source']
        dialog = FeatureDialog(None, kind, 'metre', elevation_unit='ft')
        dialog.add_values((0, 30.48, 60.96, 3.048, 1.524))
        # A new dialog already has one blank row.
        dialog.table.removeRow(0)
        for i, expected in enumerate([30.48, 60.96, 10, 5]):
            self.assertAlmostEqual(float(dialog.table.item(0, i).text()), expected)
        dialog.apply_raster_elevations([6.096])
        self.assertEqual(float(dialog.table.item(0, 2).text()), 20)
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
        row = vertex_rows(dialog.geometry, kind)[0]
        for actual, expected in zip(row, (0, 30.48, 60.96, 6.096, 1.524)):
            self.assertAlmostEqual(actual, expected)

    def test_feet_building_roof_modes_and_spacing_are_metres_on_save(self):
        kind = LAYER_TYPES['building']
        record = self.store.add_layer(kind.key, 'Building')
        layer = load_layer(self.store, record)
        feature = add_feature(layer, kind)
        dialog = FeatureDialog(None, kind, 'metre', feature, elevation_unit='ft')
        dialog.height_mode.setCurrentIndex(dialog.height_mode.findData('absolute'))
        dialog.height_value.setValue(1510)
        dialog.validate()
        self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
        for row in vertex_rows(dialog.geometry, kind):
            self.assertAlmostEqual(row[3]+row[4], 1510, places=5)
        area = FeatureDialog(None, LAYER_TYPES['area_receiver'], 'metre', elevation_unit='ft')
        area.spacing_m.lineEdit().setText('10 m')
        area.spacing_m.interpretText()
        self.assertEqual(area.object_attributes()['spacing_m'], 10)
        self.assertEqual(area.height_m.suffix(), ' ft')

    def test_project_button_refreshes_live_provider_crs(self):
        from qgis.PyQt.QtGui import QColor
        from qgis.core import QgsPalLayerSettings, QgsVectorLayerSimpleLabeling

        record = self.store.add_layer('point_receiver', 'Receiver')
        layer = load_layer(self.store, record)
        add_feature(layer, LAYER_TYPES['point_receiver'])
        layer.commitChanges()
        plugin = AcalcPlugin(FakeIface())
        plugin.initGui()
        try:
            plugin.open_project(str(self.path))
            live_layer = plugin.layers[record['layer_id']]
            live_layer.renderer().symbol().setColor(QColor('#c13570'))
            live_layer.setOpacity(0.37)
            labels = QgsPalLayerSettings()
            labels.fieldName = 'notes'
            live_layer.setLabeling(QgsVectorLayerSimpleLabeling(labels))
            live_layer.setLabelsEnabled(True)
            dialog = ProjectCoordinatesDialog(plugin.dock, self.store)
            dialog.crs.setCrs(self.feet_crs())
            dialog.elevation_units.setCurrentIndex(dialog.elevation_units.findData('ft'))
            with patch('acalc.plugin.ProjectCoordinatesDialog', return_value=dialog), patch.object(dialog, 'exec', return_value=QDialog.Accepted):
                plugin.dock.coordinates_button.click()
            self.assertEqual(elevation_unit(self.store), 'ft', plugin.dock.status.text())
            self.assertEqual(plugin.layers[record['layer_id']].crs().mapUnits(), self.feet_crs().mapUnits())
            self.assertAlmostEqual(next(plugin.layers[record['layer_id']].getFeatures()).geometry().asPoint().x(), 500000.125/.3048, places=6)
            self.assertIn('ft', plugin.dock.crs_label.text())
            self.assertEqual(live_layer.renderer().symbol().color().name(), '#c13570')
            self.assertAlmostEqual(live_layer.opacity(), 0.37)
            self.assertEqual(live_layer.labeling().settings().fieldName, 'notes')
            self.assertTrue(live_layer.labelsEnabled())
        finally:
            plugin.unload()

    def test_length_and_calculation_fields_round_trip_and_accept_typed_feet(self):
        from acalc.calculation_ui import ISOSettingsEditor
        editor = ISOSettingsEditor()
        before = editor.settings()
        editor.set_input_unit('ft')
        self.assertEqual(editor.settings(), before)
        # Calculation lengths are labelled metres whatever the project unit.
        field = editor.optional_fields['area_max_extent_m']
        field.setText('3.048')
        self.assertEqual(editor.settings()['area_max_extent_m'], 3.048)
        editor.set_input_unit('m')
        self.assertEqual(editor.settings()['area_max_extent_m'], 3.048)

    def test_import_source_height_units_are_explicit(self):
        from qgis.core import QgsVectorLayer, QgsFeature
        from acalc.layer_import import ImportOptions, import_layer
        source = QgsVectorLayer('PointZ?crs=EPSG:26913&field=height:double', 'Survey in feet', 'memory')
        feature = QgsFeature(source.fields())
        feature['height'] = 10
        feature.setGeometry(QgsGeometry.fromWkt('POINT Z (500000 4400000 100)'))
        source.dataProvider().addFeatures([feature])
        record, _, _ = import_layer(self.store, source, ImportOptions('point_source', 'Copy',
            ground_mode='geometry_z', height_field='height', vertical_to_m=.3048))
        row = vertex_rows(next(load_layer(self.store, record).getFeatures()).geometry(), LAYER_TYPES['point_source'])[0]
        self.assertEqual(row[1:3], (500000, 4400000))
        self.assertAlmostEqual(row[3], 30.48)
        self.assertAlmostEqual(row[4], 3.048)

    def test_crs_units_and_elevation_units_are_independent_in_all_combinations(self):
        from acalc.calculation_ui import ISOSettingsEditor
        from acalc.layer_import_dialog import ImportLayerDialog
        for crs_name, scale in (("EPSG:26913", 1.0), ("EPSG:2232", 1200/3937)):
            for vertical in ("m", "ft"):
                with self.subTest(crs=crs_name, elevation=vertical):
                    self.change(QgsCoordinateReferenceSystem(crs_name), vertical)
                    unit = input_unit(self.store)
                    self.assertAlmostEqual(length_factor(unit), scale, places=12)
                    metadata = self.store.metadata()
                    dialog = FeatureDialog(None, LAYER_TYPES['point_source'], metadata['horizontal_unit'],
                        horizontal_scale=scale, elevation_unit=vertical)
                    dialog.add_values((0, 100, 200, 3.048, 1.524))
                    dialog.table.removeRow(0)
                    self.assertEqual([float(dialog.table.item(0, i).text()) for i in (0, 1)], [100, 200])
                    dialog.validate()
                    self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
                    self.assertEqual(vertex_rows(dialog.geometry, LAYER_TYPES['point_source'])[0][1:3], (100, 200))
                    area = FeatureDialog(None, LAYER_TYPES['area_receiver'], metadata['horizontal_unit'],
                        horizontal_scale=scale, elevation_unit=vertical)
                    self.assertEqual(area.height_m.suffix(), ' ' + vertical)
                    self.assertEqual(area.spacing_m.suffix(), ' ' + str(unit))
                    area.spacing_m.lineEdit().setText('10 ' + str(unit))
                    area.spacing_m.interpretText()
                    self.assertAlmostEqual(area.spacing_m.value(), 10*scale, places=10)
                    editor = ISOSettingsEditor()
                    before = editor.settings()
                    editor.set_input_unit(unit)
                    self.assertEqual(editor.settings(), before)
                    imported = ImportLayerDialog(None, self.store)
                    self.assertEqual(imported.ground.value.suffix(), ' ' + vertical)
                    self.assertEqual(imported.spacing.suffix(), ' ' + str(unit))
                    self.assertEqual(elevation_unit(ModelStore.open(self.path)), vertical)

    def test_legacy_unit_preference_becomes_elevation_only(self):
        metadata = self.store.metadata()
        self.store.update_metadata(metadata['name'], '', '', '', {'input_length_unit': 'ft'})
        self.assertEqual(str(input_unit(self.store)), 'm')
        self.assertEqual(elevation_unit(self.store), 'ft')
        self.change(QgsCoordinateReferenceSystem(metadata['crs_wkt']), 'ft')
        extra = json.loads(self.store.metadata()['metadata_json'])
        self.assertEqual(extra, {'elevation_unit': 'ft'})

    def test_project_dialog_derives_horizontal_units_and_uses_meters_spelling(self):
        from acalc.dialogs import NewProjectDialog
        for dialog in (ProjectCoordinatesDialog(None, self.store),
                       NewProjectDialog(None, QgsCoordinateReferenceSystem('EPSG:26913'))):
            self.assertIn('meter', dialog.horizontal_units.text().lower())
            self.assertEqual(dialog.elevation_units.itemText(0), 'Meters')
            dialog.crs.setCrs(QgsCoordinateReferenceSystem('EPSG:2232'))
            self.assertIn('feet', dialog.horizontal_units.text().lower())
            self.assertEqual(dialog.elevation_units.currentData(), 'm')
